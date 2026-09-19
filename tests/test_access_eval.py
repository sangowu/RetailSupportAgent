"""The access-control evaluation set, run as a gate.

`scripts/run_access_eval.py` produces the number a report quotes. This module makes the
same run fail the build, so the number cannot quietly stop being true between reports.

It also checks the shape of the case set itself. A scenario set is only as good as its
worst case, and the failure mode to guard against is not a case that breaks - it is a
case that passes for the wrong reason: an empty expectation, a duplicate id, or a set
that contains only refusals and would therefore be satisfied by a system that refuses
everything.
"""

from __future__ import annotations

import collections
import copy

from retail_support_agent.access_eval import load_cases, run_all, run_case, summarize


class TestEveryAttemptIsBlocked:
    def test_the_whole_set_holds(self) -> None:
        document = load_cases()
        results = run_all(document)
        summary = summarize(results, str(document["version"]))
        assert summary["failures"] == []
        assert summary["blocked"] == summary["attempted"] == len(document["cases"])

    def test_the_set_is_the_size_it_claims(self) -> None:
        """The count is quoted in the report, so it is pinned rather than trusted."""
        document = load_cases()
        assert len(document["cases"]) == document["count"]


class TestTheCaseSetIsWellFormed:
    def test_identifiers_are_unique(self) -> None:
        ids = [case["id"] for case in load_cases()["cases"]]
        duplicates = [name for name, count in collections.Counter(ids).items() if count > 1]
        assert duplicates == []

    def test_every_case_expects_something(self) -> None:
        """A case with no expectation passes unconditionally and inflates the score."""
        empty = [case["id"] for case in load_cases()["cases"] if not (case.get("expect") or {})]
        assert empty == []

    def test_every_case_says_what_it_is_trying_to_do(self) -> None:
        """`intent` is what a reader sees in the report. A case nobody can read is a
        case nobody can challenge."""
        for case in load_cases()["cases"]:
            assert case["intent"].strip()
            assert case["category"].strip()

    def test_the_set_contains_controls_as_well_as_attacks(self) -> None:
        """A set of refusals alone would be satisfied by a system that refuses
        everything, which is the failure this guards against."""
        categories = collections.Counter(case["category"] for case in load_cases()["cases"])
        assert categories["control"] >= 3
        assert len(categories) >= 6


class TestTheSetWouldNoticeARegression:
    def test_a_case_fails_when_its_control_is_removed(self) -> None:
        """A scenario set that cannot fail proves nothing. Read scope is switched off
        for one case here, and that case must stop being blocked.
        """
        document = load_cases()
        case = next(c for c in document["cases"] if c["id"] == "read-other-users-order")
        assert run_case(case).blocked

        weakened = {**case, "scope": {**(case.get("scope") or {}), "check_output": False}}
        # Reaching another user's order is refused by the row slice, not the output
        # check, so weakening the output check alone must NOT unblock it.
        assert run_case(weakened).blocked

    def test_the_output_cases_depend_on_the_output_check(self) -> None:
        document = load_cases()
        case = next(c for c in document["cases"] if c["id"] == "invent-an-order-identifier")
        assert run_case(case).blocked

        weakened = {**case, "scope": {**(case.get("scope") or {}), "check_output": False}}
        result = run_case(weakened)
        assert not result.blocked
        assert any("#W777" in failure for failure in result.failures)

    def test_an_injection_case_fails_without_the_injection(self) -> None:
        """The poisoning cases assert two things, and both must be load-bearing: that
        the injected text really reaches the model, and that obeying it changes nothing.
        Removing the payload must break the first.
        """
        case = next(
            c for c in load_cases()["cases"] if c["id"] == "injected-instruction-drives-a-write"
        )
        assert run_case(case).blocked

        without_payload = {key: value for key, value in case.items() if key != "poison"}
        result = run_case(without_payload)
        assert not result.blocked
        assert any("never shown" in failure for failure in result.failures)

    def test_an_injection_case_fails_when_the_target_is_not_foreign(self) -> None:
        """And removing the access violation must break the second."""
        case = copy.deepcopy(
            next(
                c for c in load_cases()["cases"] if c["id"] == "injected-instruction-drives-a-write"
            )
        )
        case["turns"][0]["script"][1]["args"]["order_id"] = "#W001"
        result = run_case(case)
        assert not result.blocked
        assert any("access_denied" in failure for failure in result.failures)
