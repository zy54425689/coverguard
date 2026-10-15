# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
from dataclasses import dataclass
from genlayer import *

# ---------------------------------------------------------------- storage ---


@allow_storage
@dataclass
class Policy:
    holder: Address       # the insured party (pays the premium)
    underwriter: Address  # the risk taker (locks the coverage collateral)
    hazard_url: str       # the data source the jury must read
    condition: str        # natural-language trigger, e.g. "the page reports rainfall over 50mm"
    coverage: u256        # GEN the underwriter locked; paid to the holder if the trigger fires
    premium: u256         # GEN the holder paid; kept by the underwriter if the trigger does not fire
    state: u8             # 0=AWAITING_UNDERWRITER 1=ACTIVE 2=JUDGED 3=SETTLED 4=CANCELLED
    triggered: bool       # verdict: did the hazard condition occur?
    ruling_reason: str
    ruling_confidence: str
    paid: bool


# external message interface for paying an EOA / EVM address on the chain layer
@gl.evm.contract_interface
class _Payee:
    class View:
        pass

    class Write:
        pass


# ------------------------------------------------------------------ contract --

class CoverGuard(gl.Contract):
    """Parametric insurance with on-chain adjudication.

    Traditional parametric cover needs a trusted data oracle. CoverGuard replaces it with
    GenLayer's LLM consensus: the policy names a data-source URL and a trigger condition in
    plain language, and when a claim is judged the contract fetches that page ON-CHAIN and
    an LLM jury decides whether the trigger fired.

    Money is real in both directions:
      - the holder pays a premium (payable), locked by the contract;
      - the underwriter locks the coverage collateral (payable);
      - if the trigger fired, the coverage is transferred to the holder;
      - if it did not, the premium goes to the underwriter and the collateral is returned.
    """

    policies: TreeMap[u256, Policy]
    next_id: u256

    def __init__(self):
        self.next_id = u256(1)

    # -------------------------------------------------------- internals ----

    def _to_addr(self, value) -> Address:
        if isinstance(value, Address):
            return value
        return Address(value)

    def _addr_bytes(self, value) -> bytes:
        b = getattr(value, "as_bytes", None)
        if b is None:
            b = getattr(value, "_as_bytes", None)
        if callable(b):
            b = b()
        if isinstance(b, (bytes, bytearray)):
            return bytes(b)
        return bytes(self._to_addr(value))

    def _pay(self, to: Address, amount: u256):
        """Send GEN out of this contract (external message, executed on finalization)."""
        if amount <= u256(0):
            return
        _Payee(to).emit_transfer(value=amount)

    # ------------------------------------------------------------- views ----

    @gl.public.view
    def get_policy(self, policy_id: u256) -> dict:
        p = self.policies[policy_id]
        return {
            "id": policy_id,
            "holder": "0x" + self._addr_bytes(p.holder).hex(),
            "underwriter": "0x" + self._addr_bytes(p.underwriter).hex(),
            "hazard_url": p.hazard_url,
            "condition": p.condition,
            "coverage": p.coverage,
            "premium": p.premium,
            "state": p.state,
            "triggered": p.triggered,
            "ruling_reason": p.ruling_reason,
            "ruling_confidence": p.ruling_confidence,
            "paid": p.paid,
            "contract_balance": self.balance,
        }

    @gl.public.view
    def get_policy_count(self) -> u256:
        return self.next_id - u256(1)

    @gl.public.view
    def get_contract_balance(self) -> u256:
        return self.balance

    # ------------------------------------------------------------ writes ----

    @gl.public.write.payable
    def create_policy(self, underwriter: Address, hazard_url: str, condition: str) -> u256:
        """Open a policy and pay the premium (payable). The underwriter must opt in next."""
        value = gl.message.value
        if value <= u256(0):
            raise gl.vm.UserError("Premium must be funded: send GEN with the call")
        if len(hazard_url.strip()) == 0 or not (
                hazard_url.startswith("http://") or hazard_url.startswith("https://")):
            raise gl.vm.UserError("A valid hazard data-source URL is required")
        if len(condition.strip()) == 0:
            raise gl.vm.UserError("A trigger condition is required")

        holder = self._to_addr(gl.message.sender_address)
        uw = self._to_addr(underwriter)
        if self._addr_bytes(holder) == self._addr_bytes(uw):
            raise gl.vm.UserError("Holder and underwriter must differ")

        p = Policy(
            holder=holder,
            underwriter=uw,
            hazard_url=hazard_url,
            condition=condition,
            coverage=u256(0),
            premium=value,
            state=u8(0),
            triggered=False,
            ruling_reason="",
            ruling_confidence="",
            paid=False,
        )
        pid = self.next_id
        self.policies[pid] = p
        self.next_id = pid + u256(1)
        return pid

    @gl.public.write.payable
    def underwrite_policy(self, policy_id: u256):
        """The named underwriter locks the coverage collateral (payable). Activates the policy."""
        if policy_id not in self.policies:
            raise gl.vm.UserError("Policy not found")
        p = self.policies[policy_id]
        if p.state != u8(0):
            raise gl.vm.UserError("Policy is not awaiting an underwriter")
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(p.underwriter):
            raise gl.vm.UserError("Only the named underwriter can take this policy")

        value = gl.message.value
        if value <= p.premium:
            raise gl.vm.UserError("Collateral must exceed the premium collected")

        p.coverage = value
        p.state = u8(1)

    @gl.public.write
    def refund_unaccepted(self, policy_id: u256):
        """Holder reclaims the premium if the underwriter never took the policy."""
        if policy_id not in self.policies:
            raise gl.vm.UserError("Policy not found")
        p = self.policies[policy_id]
        if p.state != u8(0):
            raise gl.vm.UserError("Policy is not awaiting an underwriter")
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(p.holder):
            raise gl.vm.UserError("Only the holder can refund")

        amount = p.premium
        p.premium = u256(0)
        p.state = u8(4)
        p.paid = True
        self._pay(p.holder, amount)

    @gl.public.write
    def judge_claim(self, policy_id: u256):
        """The LLM jury reads the hazard data source on-chain and rules if the trigger fired."""
        if policy_id not in self.policies:
            raise gl.vm.UserError("Policy not found")
        p = self.policies[policy_id]
        if p.state != u8(1):
            raise gl.vm.UserError("Policy is not active")

        # Snapshot into plain Python values outside the nondet block.
        url_mem = str(p.hazard_url)
        cond_mem = str(p.condition)
        coverage_mem = int(p.coverage)
        premium_mem = int(p.premium)

        def _fetch_text(url: str, limit: int = 800) -> str:
            try:
                resp = gl.nondet.web.get(url)
                if isinstance(resp, bytes):
                    raw = resp.decode("utf-8", errors="ignore")
                elif hasattr(resp, "body"):
                    raw = resp.body.decode("utf-8", errors="ignore")
                else:
                    raw = str(resp)
                return " ".join(raw.split())[:limit]
            except Exception:
                return "[could not fetch this source]"

        def leader_fn():
            page = _fetch_text(url_mem)
            prompt = (
                "You are the impartial adjudicator of a parametric insurance claim.\n\n"
                "POLICY TERMS\n"
                "Trigger condition (authoritative, written by the parties): " + cond_mem + "\n"
                "Data source: " + url_mem + "\n"
                "Coverage at stake: " + str(coverage_mem) + " wei of GEN\n"
                "Premium paid: " + str(premium_mem) + " wei of GEN\n\n"
                "CONTENT FETCHED FROM THE DATA SOURCE RIGHT NOW:\n" + page + "\n\n"
                "Decide whether the trigger condition has occurred, judging ONLY on the fetched "
                "content above. If the page is unreachable, empty, or does not contain the data "
                "the condition refers to, the trigger has NOT occurred.\n\n"
                'Respond as JSON with exactly these keys:\n'
                '- "triggered": true or false\n'
                '- "reason": concise 2-4 sentence justification citing the fetched content\n'
                '- "confidence": one of "high", "medium", "low"'
            )
            return gl.nondet.exec_prompt(prompt, response_format='json')

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            mine = leader_fn()
            if not isinstance(mine, dict) or not isinstance(leaders_res.calldata, dict):
                return False
            a = mine.get("triggered")
            b = leaders_res.calldata.get("triggered")
            if isinstance(a, str):
                a = a.strip().lower() == "true"
            if isinstance(b, str):
                b = b.strip().lower() == "true"
            return bool(a) == bool(b)

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        trig = result.get("triggered", False)
        if isinstance(trig, str):
            trig = trig.strip().lower() == "true"
        p.triggered = bool(trig)
        p.ruling_reason = str(result.get("reason", ""))
        p.ruling_confidence = str(result.get("confidence", ""))
        p.state = u8(2)

    @gl.public.write
    def settle(self, policy_id: u256):
        """Pay out according to the verdict. Moves real GEN in both directions."""
        if policy_id not in self.policies:
            raise gl.vm.UserError("Policy not found")
        p = self.policies[policy_id]
        if p.paid:
            raise gl.vm.UserError("Policy already settled")
        if p.state != u8(2):
            raise gl.vm.UserError("Claim has not been judged yet")

        coverage = p.coverage
        premium = p.premium
        p.coverage = u256(0)
        p.premium = u256(0)
        p.paid = True
        p.state = u8(3)

        if p.triggered:
            # the hazard occurred: the holder collects the coverage, underwriter keeps the premium
            self._pay(p.holder, coverage + premium)
        else:
            # no hazard: the underwriter keeps the premium and gets the collateral back
            self._pay(p.underwriter, coverage + premium)
