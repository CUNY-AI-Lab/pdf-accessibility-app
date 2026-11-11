"""Per-test olmOCR-Bench results and the paired comparison of two candidates."""

from types import SimpleNamespace

from scripts.score_olmocr import MISSING, compare, evaluate


def result(test_id: str, passed: bool) -> dict:
    return {
        "id": test_id,
        "pdf": f"s/{test_id}.pdf",
        "page": 1,
        "type": "present",
        "passed": passed,
    }


class StubTest(SimpleNamespace):
    def run(self, text: str):
        if self.raises:
            raise ValueError("bad table")
        return self.expected in text, "" if self.expected in text else "not found"


def stub(test_id: str, expected: str = "word", raises: bool = False) -> StubTest:
    return StubTest(
        id=test_id, pdf=f"s/{test_id}.pdf", page=1, type="present", expected=expected, raises=raises
    )


def test_evaluate_fails_missing_pages_and_tests_that_raise():
    pages = {"s/a.pdf": "a word here", "s/c.pdf": "word"}
    results = evaluate(
        [stub("a"), stub("b"), stub("c", raises=True)],
        lambda pdf, page: pages.get(pdf),
    )
    assert [(entry["id"], entry["passed"]) for entry in results] == [
        ("a", True),
        ("b", False),
        ("c", False),
    ]
    assert results[1]["explanation"] == MISSING
    assert results[2]["explanation"] == "bad table"


def test_compare_pairs_tests_by_id():
    base = [result("a", True), result("b", False), result("c", True), result("d", False)]
    candidate = [result("a", True), result("b", True), result("c", False), result("e", True)]
    change = compare(base, candidate)
    assert [entry["id"] for entry in change["fixed"]] == ["b"]
    assert [entry["id"] for entry in change["broken"]] == ["c"]
    assert change["both_passed"] == 1
    assert change["both_failed"] == 0
    assert change["unpaired"] == 2
