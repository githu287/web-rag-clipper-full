"""Offline comparison with independent subtitles; no models or network calls.

English words and individual CJK characters are tokens. Public subtitles often
omit fillers, so token error rate measures disagreement, not certified accuracy.
Cue windows near cuts are approximate: subtitle cue times are not word times.
"""
import html
import math
import re
import unicodedata


def tokens(text):
    text = html.unescape(re.sub(r"<[^>]*>", "", text)).casefold().replace("’", "'")
    return re.findall(r"[\u3400-\u9fff]|[^\W_\u3400-\u9fff]+(?:'[^\W_\u3400-\u9fff]+)*", text)


def read_vtt(text):
    """Read plain VTT cues, ignoring speaker labels and non-speech annotations."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    if not text.startswith("WEBVTT"):
        raise ValueError("Reference must be WEBVTT")
    def seconds(value):
        parts = value.split(":")
        if len(parts) not in (2, 3):
            raise ValueError("Invalid VTT timestamp")
        result = sum(float(part) * 60 ** index for index, part in enumerate(reversed(parts)))
        if not math.isfinite(result) or result < 0:
            raise ValueError("Invalid VTT timestamp")
        return result
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.splitlines()
        if lines and lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        position = next((i for i, line in enumerate(lines) if " --> " in line), None)
        if position is None:
            continue
        timing = lines[position].split(" --> ")
        start, end = seconds(timing[0].strip()), seconds(timing[1].split()[0])
        if end <= start or (cues and start < cues[-1]["start_seconds"]):
            raise ValueError("Reference cue times must be ordered and nonempty")
        value = html.unescape(re.sub(r"<[^>]*>", "", " ".join(lines[position + 1:])))
        value = re.sub(r"^[A-Z][A-Z .'-]{0,60}:\s*", "", value)
        value = re.sub(r"\[[^\]]*\]", "", value).strip()
        if value:
            cues.append({"start_seconds": start, "end_seconds": end, "text": value})
    if not cues:
        raise ValueError("Reference contains no speech cues")
    return cues


def disagreement(reference, hypothesis):
    """Exact Levenshtein token distance; do not round percentages into pass/fail."""
    if not reference:
        return {"reference_tokens": 0, "hypothesis_tokens": len(hypothesis), "edit_distance": len(hypothesis), "token_error_rate": None}
    # Bound work for accidentally supplied books/full-day transcripts.
    if len(reference) > 12000 or len(hypothesis) > 12000:
        raise ValueError("Reference comparison exceeds 12000 tokens")
    previous = list(range(len(hypothesis) + 1))
    for index, expected in enumerate(reference, 1):
        current = [index]
        for column, actual in enumerate(hypothesis, 1):
            current.append(min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (expected != actual)))
        previous = current
    distance = previous[-1]
    return {"reference_tokens": len(reference), "hypothesis_tokens": len(hypothesis), "edit_distance": distance,
            "token_error_rate": distance / len(reference)}


def compare_reference_text(reference, hypothesis):
    """Chinese-first CER from independent text; never manufacture subtitle times."""
    if "\ufffd" in reference:
        raise ValueError("Reference contains Unicode replacement characters; repair its source encoding before scoring")
    def characters(text):
        text = unicodedata.normalize("NFKC", text).casefold().replace("−", "-")
        result = []
        for index, char in enumerate(text):
            previous = text[index - 1] if index else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            if (unicodedata.category(char)[0] in ("L", "N") or char in "%‰°\ufffd"
                or (char in ".:/" and previous.isdigit() and following.isdigit())
                or (char in "+-" and following.isdigit())):
                result.append(char)
        return result
    expected, actual = characters(reference), characters(hypothesis)
    if not expected:
        raise ValueError("Reference has no letters, numbers or Chinese characters")
    score = disagreement(expected, actual)
    return {"reference_characters": len(expected), "hypothesis_characters": len(actual),
            "edit_distance": score["edit_distance"], "character_error_rate": score["token_error_rate"],
            "method": "NFKC/case normalized character edit distance; keep numbers, names, decimal/sign/ratio and percent units; no timestamp reference"}


def compare_transcript(cues, sentences, duration, segment_seconds=90):
    """Compare whole text and matched cue windows around every segment boundary.

    Only complete reference cues inside the tested range are scored. Hypothesis
    excerpts overlapping the last cue boundary may contain extra words; report
    that limitation rather than assigning fabricated per-word subtitle times.
    """
    selected = [c for c in cues if 0 <= c["start_seconds"] < c["end_seconds"] <= duration]
    if not selected:
        raise ValueError("No complete reference cues inside tested media")
    text = lambda rows: tokens(" ".join(row["text"] for row in rows))
    score = disagreement(text(selected), text(sentences))
    # Exact, unique cue phrases can independently check range overlap. Do not
    # interpolate word times inside an ASR excerpt or hide unmatched phrases.
    actual_tokens, owners = [], []
    for number, sentence in enumerate(sentences):
        value = tokens(sentence["text"])
        actual_tokens.extend(value)
        owners.extend([number] * len(value))
    joined = " " + " ".join(actual_tokens) + " "
    anchors, candidates = [], 0
    for cue in selected:
        value = tokens(cue["text"])
        if len(value) < 5:
            continue
        candidates += 1
        needle = " " + " ".join(value) + " "
        position = joined.find(needle)
        if position < 0 or joined.find(needle, position + 1) >= 0:
            continue
        index = joined[:position].count(" ")
        first, last = sentences[owners[index]], sentences[owners[index + len(value) - 1]]
        overlap = first["start_seconds"] < cue["end_seconds"] and last["end_seconds"] > cue["start_seconds"]
        anchors.append({"reference_start": cue["start_seconds"], "reference_end": cue["end_seconds"],
                        "asr_start": first["start_seconds"], "asr_end": last["end_seconds"], "overlap": overlap})
    boundaries = []
    for cut in range(segment_seconds, math.ceil(duration), segment_seconds):
        reference = [c for c in selected if c["start_seconds"] < cut + 3 and c["end_seconds"] > cut - 3]
        if not reference:
            continue
        left, right = reference[0]["start_seconds"], reference[-1]["end_seconds"]
        actual = [s for s in sentences if s["start_seconds"] < right and s["end_seconds"] > left]
        boundaries.append({"cut_seconds": cut, "reference_start": left, "reference_end": right,
                           **disagreement(text(reference), text(actual))})
    return {**score, "reference_cues": len(selected), "boundaries": boundaries,
            "cue_alignment": {"candidate_cues": candidates, "uniquely_matched_cues": len(anchors),
                              "overlapping_ranges": sum(a["overlap"] for a in anchors), "anchors": anchors},
            "method": "Case/punctuation normalized token edit distance; no numeric/name correction; cue windows are approximate"}
