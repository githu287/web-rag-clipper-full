"""Resolve bounded model references only against server-owned evidence IDs."""
import re


def citation_ids(answer, kind, valid_ids, *, strict=False):
    result = set()
    # Models also write ranges as "[原文 224]至[原文 227]". Both
    # endpoints explicitly denote the intervening evidence; validate the whole
    # bounded range rather than silently reviewing/displaying just endpoints.
    label = re.escape(kind)
    endpoint = r"\[" + label + r"\s*[0-9]{1,4}\]"
    def normalize_range(match):
        bounds = [int(n) for n in re.findall(r"[0-9]{1,4}", match[0])]
        if any(left > right for left, right in zip(bounds, bounds[1:])):
            return f"[{kind} invalid]"
        return f"[{kind} {bounds[0]}-{bounds[-1]}]"
    answer = re.sub(endpoint + r"(?:\s*[-–—至到]\s*" + endpoint + r")+", normalize_range, answer)
    item = r"[0-9]{1,4}(?:\s*[-–—至]\s*[0-9]{1,4})?"
    for match in re.finditer(r"\[" + re.escape(kind) + r"\s*([^\[\]]*)\]", answer):
        value = match.group(1).strip()
        valid = len(value) <= 160 and re.fullmatch(item + r"(?:\s*[,，、]\s*" + item + r")*", value)
        referenced = set()
        if valid:
            for part in re.split(r"[,，、]", value):
                bounds = re.split(r"\s*[-–—至]\s*", part.strip())
                start, end = int(bounds[0]), int(bounds[-1])
                if not 1 <= start <= end or end - start >= 20:
                    valid = False
                    break
                referenced.update(range(start, end + 1))
        if valid and len(referenced) <= 20 and referenced <= valid_ids:
            result.update(referenced)
        elif strict:
            raise ValueError("Unresolvable citation")
    return result
