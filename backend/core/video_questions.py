"""Server-owned question slots and conservative rendering of scoped claims."""
import re
import math

from .video_errors import VideoError


def question_parts(query):
    """Split explicit questions, keeping quoted question marks as source text.

    This is punctuation/numbering handling, not a semantic question planner.
    The unchanged full query is still supplied as context to every slot.
    """
    numbered = list(re.finditer(r"(?m)^\s*(?:[0-9]{1,2}[.、)）]|[一二三四五六][、）])\s*", query))
    if len(numbered) >= 2:
        parts = [query[m.start():numbered[i + 1].start() if i + 1 < len(numbered) else len(query)].strip()
                 for i, m in enumerate(numbered)]
    else:
        parts, start, stack = [], 0, []
        pairs = {'“': '”', '‘': '’', '「': '」', '『': '』', '（': '）', '(': ')'}
        for i, char in enumerate(query):
            if stack:
                if char == stack[-1]:
                    stack.pop()
                elif char in pairs:
                    stack.append(pairs[char])
            elif char in pairs:
                stack.append(pairs[char])
            elif char in ('"', '`'):
                stack.append(char)
            elif char in '？?' and (i + 1 == len(query) or query[i + 1] not in '？?'):
                parts.append(query[start:i + 1].strip())
                start = i + 1
        tail = query[start:].strip()
        if not parts:
            parts = [query.strip()]
        elif tail:
            parts[-1] += ' ' + tail
    if len(parts) > 6:
        raise VideoError("一次最多回答6个明确分开的问句，请分批提问", 422)
    return parts


def scoped_claim(answer):
    """Conservative lexical trigger, not a general entailment decision."""
    return bool(re.search(r"未|没有|从不|不曾|并不|并非|并无|不是|不在|不能|不得|不允许|不可以|不要|禁止|仅|只|唯一|始终|一直|从来|全部|所有|绝对|不实|不符|不成立"
                          r"|\b(?:not|no|never|only|always|all|every|none|exclusively|cannot|can't|won't|isn't|doesn't|didn't|hasn't|don't)\b", answer.replace('’', "'"), re.I))


def scoped_question(query):
    # An untrusted premise may choose a conservative display route, but never
    # supply a fact, entity or negation to the reviewer's evidence.
    return bool(re.search(r"对吗|是否|是不是|有没有|能否|能不能|可否|可不可以|可以.*[吗么]|正确吗|属实吗|唯一|从未|哪[所家位]|姓名|名字|名称"
                          r"|^\s*(?:is|are|was|were|do|does|did|has|have|can|could)\b", query, re.I))


def excerpt_ids(cited_ids, sentences):
    """One adjacent row on each side, labelled separately, never an inference.

    Context retrieval is allowed only on the literal display path; semantic
    review still sees exclusively the original explicitly cited IDs.
    """
    by_id = {row['id']: row for row in sentences}
    selected = set(cited_ids)
    def close(left, right):
        values = [left.get('start_seconds'), left.get('end_seconds'), right.get('start_seconds'), right.get('end_seconds')]
        return (all(type(v) in (int, float) and math.isfinite(v) for v in values)
                and 0 <= values[0] < values[1] <= values[2] < values[3]
                and values[2] - values[1] <= 2 and values[3] - values[0] <= 30)
    for i in sorted(cited_ids):
        for neighbour in (i - 1, i + 1):
            if len(selected) >= 6:
                return selected
            if neighbour in by_id and neighbour not in selected:
                left, right = by_id[min(i, neighbour)], by_id[max(i, neighbour)]
                if close(left, right):
                    selected.add(neighbour)
    return selected


def render_answers(answers):
    if len(answers) == 1:
        return answers[0]
    return "\n\n".join(f"{i + 1}. {text}" for i, text in enumerate(answers))


def source_excerpt(raw_ids, originals, kind="原文"):
    """Only server-owned text leaves this path, with no inferred conclusion."""
    if not raw_ids or len(raw_ids) > 6 or sum(len(originals[i]) for i in raw_ids) > 3500:
        return None
    # Source text must not introduce extra live citation labels. The API's
    # sources retain exact original text; only brackets in display are escaped.
    rows = [originals[i].replace('[', '［').replace(']', '］') +
            (" [整体摘要]" if kind == "整体摘要" else f" [{kind} {i}]") for i in sorted(raw_ids)]
    basis = {"原文": "原文", "片段": "AI分段总结", "整体摘要": "AI整体摘要"}[kind]
    return f"以下为所引{basis}摘录，未据此追加排他或范围外判断：\n\n" + "\n\n".join(rows)
