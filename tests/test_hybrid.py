from opr_mcp.search.hybrid import _rrf


def test_rrf_combines_ranks():
    a = [(1, 10.0), (2, 5.0), (3, 1.0)]
    b = [(2, 9.0), (3, 8.0), (1, 7.0)]
    fused = _rrf([a, b])
    # 1: rank 1 in a, rank 3 in b. score = 1/61 + 1/63
    # 2: rank 2 in a, rank 1 in b. score = 1/62 + 1/61
    # 3: rank 3 in a, rank 2 in b. score = 1/63 + 1/62
    expected_1 = 1 / 61 + 1 / 63
    expected_2 = 1 / 62 + 1 / 61
    assert abs(fused[1] - expected_1) < 1e-9
    assert abs(fused[2] - expected_2) < 1e-9
    # The chunk that was top-1 in both lists wins.
    top = max(fused, key=fused.get)
    assert top == 2


def test_rrf_handles_singleton_lists():
    fused = _rrf([[(1, 1.0)]])
    assert list(fused.keys()) == [1]
    assert fused[1] == 1 / 61
