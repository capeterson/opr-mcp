from opr_mcp.search.query import preprocess


def test_preprocess_strips_parametric_rules():
    p = preprocess("How does Tough(3) interact with AP(2)?")
    assert "Tough" in p.text
    assert "AP" in p.text
    assert "(3)" not in p.text
    assert "(2)" not in p.text
    assert set(p.rule_names) == {"Tough", "AP"}


def test_preprocess_passthrough_simple():
    p = preprocess("furious melee bonus")
    assert p.text == "furious melee bonus"
    assert p.rule_names == ()


def test_preprocess_dedups_rule_names():
    p = preprocess("AP(1) and AP(2) and AP(3)")
    assert p.rule_names == ("AP",)
