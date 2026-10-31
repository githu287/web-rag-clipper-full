"""Server-owned question slots and conservative rendering of scoped claims."""
import re
import math

from .video_errors import VideoError


def visible_text(text):
    """Mask balanced source quotes/code without changing character offsets.

    An unmatched delimiter cannot swallow the rest of a question or its quota.
    This is a small punctuation scanner, not a Markdown or semantic parser.
    """
    visible, stack, i = list(text), [], 0
    pairs = {'“': '”', '‘': '’', '「': '」', '『': '』', '（': '）', '(': ')', '[': ']'}
    while i < len(text):
        char = text[i]
        if stack:
            close, start, opaque = stack[-1]
            apostrophe = close == "'" and i and i + 1 < len(text) and text[i - 1].isalnum() and text[i + 1].isalnum()
            if text.startswith(close, i) and not apostrophe:
                stack.pop()
                end = i + len(close)
                numeric_marker = text[start] in '(（' and re.fullmatch(r"[\d一二三四五六七八九十]{1,3}", text[start + 1:i])
                if not numeric_marker:
                    visible[start:end] = [' '] * (end - start)
                i = end
                continue
            if opaque:
                i += 1
                continue
        if char == '`':
            token = re.match(r"`+", text[i:])[0]
            stack.append((token, i, True))
            i += len(token)
            continue
        if char in ('"', "'") and not (char == "'" and i and text[i - 1].isalnum()):
            stack.append((char, i, True))
        elif char in pairs:
            stack.append((pairs[char], i, char in '“‘「『'))
        i += 1
    return ''.join(visible)


def question_like(text):
    text = visible_text(text).strip(' \n\t,，、;；?？')
    return bool(re.search(r"什么|谁|哪[里儿个所家位些一间]|多少|几[年月天点个位家种]|何[时处人地]|为何|为什么|怎么|怎样|如何|是否|能否|可否|能不能|有没有|是不是|对吗|吗$"
                          r"|\b(?:what|where|which|who|how|why|when)\b|^\s*(?:is|are|was|were|do|does|did|can|could)\b", text, re.I))


def _directive(text):
    return not question_like(text) and bool(re.match(
        r"\s*(?:请(?:(?:分别|逐项)?(?:引用|标注|注明)|给出(?:出处|来源)|提供(?:出处|来源)|按原文)|每[问项]|不要补充|不用补充|只回答|please\s+(?:cite|quote|use))", text, re.I))


def _separate(text, separators):
    visible, parts, start = visible_text(text), [], 0
    for i, char in enumerate(visible):
        if char in separators and not (char in '？?' and i + 1 < len(visible) and visible[i + 1] in '？?'):
            if text[start:i + 1].strip():
                parts.append(text[start:i + 1].strip())
            start = i + 1
    if text[start:].strip():
        parts.append(text[start:].strip())
    return parts or [text.strip()]


def _clauses(text, explicit=False):
    strong = _separate(text, ';；\n')
    content = [p for p in strong if visible_text(p).strip() and not _directive(p)]
    if len(content) >= 2 and (explicit or all(question_like(p) for p in content)):
        groups = []
        for part in strong:
            if _directive(part) and groups:
                groups[-1] += ' ' + part
            else:
                groups.append(part)
    else:
        groups = [text]
    result = []
    for group in groups:
        commas = _separate(group, ',，')
        # An ordinary list ("大学、医院和地点分别是什么") remains one
        # question. Every comma clause must independently ask something.
        if len(commas) >= 2 and all(question_like(p) for p in commas):
            result.extend(commas)
        else:
            result.append(group.strip())
    return result


def question_parts(query):
    """Bounded slots for visible questions/lists; no paid semantic planning."""
    visible = visible_text(query)
    markers = list(re.finditer(
        r"(?m)(?:^|(?<=[;；]))[ \t]*(?:\d{1,2}(?:[、)）]|[.．](?!\d))|[一二三四五六七八九十]{1,3}[、)）]|[（(]\d{1,2}[)）]|[-*•][ \t]+)[ \t]*", visible))
    blocks = [query[m.start():markers[i + 1].start() if i + 1 < len(markers) else len(query)].strip()
              for i, m in enumerate(markers)] if len(markers) >= 2 else [query]
    if len(markers) >= 2 and question_like(query[:markers[0].start()]):
        blocks.insert(0, query[:markers[0].start()].strip())
    explicit = bool(re.search(r"分别回答|逐项回答|一问一答|依次回答", visible))
    parts = []
    for block in blocks:
        items = _separate(block, '？?')
        if len(items) > 1 and not question_like(items[-1]) and not visible_text(items[-1]).rstrip().endswith(('?', '？')):
            tail = items.pop()
            items[-1] += ' ' + tail
        for item in items:
            parts.extend(_clauses(item, explicit))
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
                          r"|(?:什么|哪个|哪一个|哪间).{0,12}(?:大学|学校|医院|机构|单位|公司)"
                          r"|\b(?:which|what).{0,50}\b(?:hospital|university|college|school|company|institution|employer|name)\b"
                          r"|^\s*(?:is|are|was|were|do|does|did|has|have|can|could)\b", query, re.I))


def excerpt_ids(cited_ids, sentences):
    """One adjacent row on each side, labelled separately, never an inference.

    Context retrieval is allowed only on the literal display path; semantic
    review still sees exclusively the original explicitly cited IDs.
    """
    by_id = {row['id']: row for row in sentences}
    selected = set(cited_ids)
    characters = sum(len(by_id[i]['text']) for i in selected)
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
                size = len(by_id[neighbour]['text'])
                if close(left, right) and characters + size <= 3500:
                    selected.add(neighbour)
                    characters += size
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
