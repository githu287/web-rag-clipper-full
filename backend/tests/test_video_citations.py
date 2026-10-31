"""Real Chinese answers may put range endpoints in separate brackets."""
import pytest

from backend.core.video_citations import citation_ids


@pytest.mark.parametrize("connector", ["至", "到", "–", "—", "-"])
@pytest.mark.parametrize("kind", ["原文", "片段"])
def test_separately_bracketed_range_includes_all_server_sources(connector, kind):
    answer = f"根据[{kind} 224] {connector} [{kind} 227]说明。"
    assert citation_ids(answer, kind, {224, 225, 226, 227}, strict=True) == {224, 225, 226, 227}


@pytest.mark.parametrize("answer,ids", [
    ("[原文 224]至[原文 227]", {224, 227}),
    ("[原文 1]至[原文 99]", set(range(1, 100))),
    ("[原文 9]至[原文 1]", set(range(1, 10))),
    ("[原文 1]至[原文 5]至[原文 3]", set(range(1, 6))),
])
def test_invalid_cross_bracket_range_is_rejected_as_a_whole(answer, ids):
    with pytest.raises(ValueError):
        citation_ids(answer, "原文", ids, strict=True)
    assert citation_ids(answer, "原文", ids) == set()


def test_separate_references_and_mixed_source_types_do_not_create_ranges():
    assert citation_ids("甲[原文 1]，乙[原文 4]", "原文", {1, 2, 3, 4}, strict=True) == {1, 4}
    assert citation_ids("[原文 1]至[片段 4]", "原文", {1, 2, 3, 4}, strict=True) == {1}
    assert citation_ids("[原文 1]至[原文 3]至[原文 5]", "原文", set(range(1, 6)), strict=True) == set(range(1, 6))
