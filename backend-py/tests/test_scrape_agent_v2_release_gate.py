from app.services.scrape_agent_v2.release_gate import evaluate


def test_missing_benchmarks_never_pass_release_gate():
    result=evaluate({"requirements":[{"id":"S001"}]},[],[],[],False)
    assert not result["ready"]
    assert result["missingCases"]==["S001"]
    assert result["accuracy"]=={}


def test_unverified_placeholders_do_not_count_as_golden_courses():
    result=evaluate({"requirements":[]},[
        {"id":i,"country":"Australia","fields":{},"review_method":"synthetic"}
        for i in range(50)],[],[],True)
    assert not result["ready"]
    assert any("50" in failure for failure in result["failures"])
